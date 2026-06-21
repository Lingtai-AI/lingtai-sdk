"""Tests for ToolExecutor — sequential and parallel tool execution."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

import lingtai_kernel.tool_executor as tool_executor_module
from lingtai_kernel.llm.base import ToolCall
from lingtai_kernel.llm.interface import ToolResultBlock
from lingtai_kernel.loop_guard import LoopGuard
from lingtai_kernel.tool_call_guard import GuardDecision, ToolCallGuard
from lingtai_kernel.tool_executor import ToolExecutor
from lingtai_kernel.types import UnknownToolError


def make_executor(
    dispatch_fn=None,
    parallel_safe=None,
    known_tools=None,
    logger_fn=None,
    working_dir=None,
    max_result_chars=50_000,
    guard=None,
    tool_call_guard=None,
):
    if dispatch_fn is None:
        dispatch_fn = lambda tc: {"status": "ok", "result": f"ran {tc.name}"}
    make_result = MagicMock(side_effect=lambda name, result, **kw: {"name": name, "result": result})
    guard = guard or LoopGuard(max_total_calls=50)
    return ToolExecutor(
        dispatch_fn=dispatch_fn,
        make_tool_result_fn=make_result,
        guard=guard,
        known_tools=known_tools,
        parallel_safe_tools=parallel_safe or set(),
        logger_fn=logger_fn,
        working_dir=working_dir,
        max_result_chars=max_result_chars,
        tool_call_guard=tool_call_guard,
    )


def _log_index(logs, event_type, *, trace_id=None):
    for index, (event, fields) in enumerate(logs):
        if event != event_type:
            continue
        if trace_id is not None and fields.get("tool_trace_id") != trace_id:
            continue
        return index
    raise AssertionError(f"missing log event {event_type!r} for trace {trace_id!r}")


def _trace_events(logs, trace_id):
    return [event for event, fields in logs if fields.get("tool_trace_id") == trace_id]


def test_execute_single_tool():
    executor = make_executor()
    calls = [ToolCall(name="read", args={"path": "/tmp"}, id="tc1")]
    results, intercepted, text = executor.execute(calls)
    assert len(results) == 1
    assert not intercepted



def test_tool_call_guard_default_allow_preserves_pass_through_log():
    logs = []
    dispatch_calls = []

    def dispatch(tc):
        dispatch_calls.append((tc.name, tc.args, tc.id))
        return {"status": "ok"}

    executor = make_executor(
        dispatch_fn=dispatch,
        known_tools={"read"},
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
    )

    results, intercepted, _ = executor.execute([
        ToolCall(name="read", args={"path": "/tmp"}, id="guard-pass"),
    ])

    assert not intercepted
    assert results[0]["result"]["status"] == "ok"
    assert dispatch_calls == [("read", {"path": "/tmp"}, "guard-pass")]
    approved = logs[_log_index(logs, "tool_call_approved", trace_id="guard-pass")][1]
    assert approved["approval_mode"] == "pass_through"
    assert approved["policy"] == "default_allow"
    assert "guard_decision" not in approved


def test_tool_call_guard_denies_before_dispatch_with_structured_rejection_result():
    logs = []
    dispatch_calls = []
    errors = []

    def deny_send(proposal):
        assert proposal.tool_name == "telegram"
        assert proposal.tool_args == {"action": "send", "text": "hi"}
        assert proposal.tool_call_id == "guard-deny"
        assert proposal.tool_trace_id == "guard-deny"
        return GuardDecision.deny(
            check_name="no_external_send",
            reason="external sends are disabled in this test",
            metadata={"category": "external_message_send"},
        )

    executor = make_executor(
        dispatch_fn=lambda tc: dispatch_calls.append(tc) or {"status": "ok"},
        known_tools={"telegram"},
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
        tool_call_guard=ToolCallGuard([deny_send]),
    )

    results, intercepted, _ = executor.execute([
        ToolCall(name="telegram", args={"action": "send", "text": "hi"}, id="guard-deny"),
    ], collected_errors=errors)

    assert not intercepted
    assert dispatch_calls == []
    payload = results[0]["result"]
    assert payload["status"] == "error"
    assert payload["error_type"] == "ToolCallGuardDenied"
    assert payload["error_phase"] == "guard"
    assert payload["message"] == "external sends are disabled in this test"
    decision = payload["guard_decision"]
    assert decision["allowed"] is False
    assert decision["check_name"] == "no_external_send"
    assert decision["action"] == "deny"
    assert decision["severity"] == "error"
    assert decision["metadata"] == {"category": "external_message_send"}
    assert decision["proposal"]["tool_name"] == "telegram"
    advisory = payload["_advisory"]
    assert advisory["type"] == "tool_call_guard"
    assert advisory["allowed"] is False
    assert advisory["message"] == "external sends are disabled in this test"
    events = _trace_events(logs, "guard-deny")
    assert "tool_call_denied" in events
    assert "tool_call_approved" not in events
    assert "tool_call_dispatch_start" not in events
    denied = logs[_log_index(logs, "tool_call_denied", trace_id="guard-deny")][1]
    assert denied["guard_decision"]["check_name"] == "no_external_send"
    assert any("external sends are disabled" in error for error in errors)



def test_tool_call_guard_denies_one_parallel_call_without_dispatching_it():
    logs = []
    dispatch_calls = []

    def deny_write(proposal):
        if proposal.tool_name == "write":
            return GuardDecision.deny(
                check_name="deny_write",
                reason="write disabled",
            )
        return GuardDecision.allow(check_name="allow_read")

    def dispatch(tc):
        dispatch_calls.append(tc.name)
        return {"status": "ok", "tool": tc.name}

    executor = make_executor(
        dispatch_fn=dispatch,
        known_tools={"read", "write"},
        parallel_safe={"read", "write"},
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
        tool_call_guard=ToolCallGuard([deny_write]),
    )

    results, intercepted, _ = executor.execute([
        ToolCall(name="read", args={"path": "/tmp"}, id="parallel-read"),
        ToolCall(name="write", args={"file_path": "x", "content": "y"}, id="parallel-write"),
    ])

    assert not intercepted
    assert dispatch_calls == ["read"]
    assert results[0]["result"]["tool"] == "read"
    denied = results[1]["result"]
    assert denied["status"] == "error"
    assert denied["error_type"] == "ToolCallGuardDenied"
    assert denied["guard_decision"]["check_name"] == "deny_write"
    assert "tool_call_denied" in _trace_events(logs, "parallel-write")
    assert "tool_call_dispatch_start" not in _trace_events(logs, "parallel-write")
    assert "tool_call_dispatch_start" in _trace_events(logs, "parallel-read")

def test_tool_call_guard_warning_allows_dispatch_and_adds_advisory():
    logs = []

    def warn(proposal):
        return GuardDecision.allow(
            check_name="warn_large_side_effect",
            reason="allowed with warning",
            action="warn",
            severity="warning",
        )

    executor = make_executor(
        known_tools={"write"},
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
        tool_call_guard=ToolCallGuard([warn]),
    )

    results, intercepted, _ = executor.execute([
        ToolCall(name="write", args={"file_path": "x", "content": "y"}, id="guard-warn"),
    ])

    assert not intercepted
    payload = results[0]["result"]
    assert payload["status"] == "ok"
    advisory = payload["_advisory"]
    assert advisory["type"] == "tool_call_guard"
    assert advisory["allowed"] is True
    assert advisory["action"] == "warn"
    assert advisory["severity"] == "warning"
    approved = logs[_log_index(logs, "tool_call_approved", trace_id="guard-warn")][1]
    assert approved["approval_mode"] == "guard"
    assert approved["policy"] == "warn_large_side_effect"
    assert approved["guard_decision"]["action"] == "warn"



def test_tool_call_guard_warning_survives_sequential_dispatch_exception():
    def warn(proposal):
        return GuardDecision.allow(
            check_name="warn_then_dispatch_fails",
            reason="allowed with warning before dispatch failure",
            action="warn",
            severity="warning",
        )

    def dispatch(tc):
        raise RuntimeError("boom")

    executor = make_executor(
        dispatch_fn=dispatch,
        known_tools={"explode"},
        tool_call_guard=ToolCallGuard([warn]),
    )

    results, intercepted, _ = executor.execute([
        ToolCall(name="explode", args={}, id="guard-warn-error"),
    ])

    assert not intercepted
    payload = results[0]["result"]
    assert payload["status"] == "error"
    assert payload["error_phase"] == "dispatch"
    assert payload["error_type"] == "RuntimeError"
    advisory = payload["_advisory"]
    assert advisory["type"] == "tool_call_guard"
    assert advisory["action"] == "warn"
    assert advisory["severity"] == "warning"
    assert advisory["check_name"] == "warn_then_dispatch_fails"


def test_tool_call_guard_check_exception_denies_without_crashing_parallel_batch():
    dispatch_calls = []

    def bad_guard(proposal):
        if proposal.tool_name == "bad":
            raise ValueError("bad guard")
        return None

    def dispatch(tc):
        dispatch_calls.append(tc.name)
        return {"status": "ok", "tool": tc.name}

    executor = make_executor(
        dispatch_fn=dispatch,
        known_tools={"bad", "ok"},
        parallel_safe={"bad", "ok"},
        tool_call_guard=ToolCallGuard([bad_guard]),
    )

    results, intercepted, _ = executor.execute([
        ToolCall(name="bad", args={}, id="guard-check-raises"),
        ToolCall(name="ok", args={}, id="guard-check-ok"),
    ])

    assert not intercepted
    assert dispatch_calls == ["ok"]
    denied = results[0]["result"]
    assert denied["status"] == "error"
    assert denied["error_phase"] == "guard"
    assert denied["error_type"] == "ToolCallGuardDenied"
    assert denied["guard_check"] == "bad_guard"
    assert "ValueError: bad guard" in denied["message"]
    assert denied["guard_decision"]["metadata"] == {"exception_type": "ValueError"}
    assert results[1]["result"]["tool"] == "ok"


def test_duplicate_and_guard_advisories_are_preserved_together():
    def warn(proposal):
        return GuardDecision.allow(
            check_name="warn_duplicate_context",
            reason="guard also has advice",
            action="warn",
            severity="warning",
        )

    executor = make_executor(
        guard=LoopGuard(max_total_calls=50, dup_free_passes=2, dup_hard_block=3),
        tool_call_guard=ToolCallGuard([warn]),
    )

    executor.execute([ToolCall(name="poll", args={}, id="multi-adv-1")])
    executor.execute([ToolCall(name="poll", args={}, id="multi-adv-2")])
    results, intercepted, _ = executor.execute([ToolCall(name="poll", args={}, id="multi-adv-3")])

    assert not intercepted
    advisory = results[0]["result"]["_advisory"]
    assert advisory["type"] == "multiple_tool_advisories"
    assert [item["type"] for item in advisory["items"]] == [
        "duplicate_tool_call",
        "tool_call_guard",
    ]
    assert advisory["items"][0]["repeat_count"] == 3
    assert advisory["items"][1]["check_name"] == "warn_duplicate_context"


def test_lifecycle_trace_events_for_sequential_success():
    logs = []

    def dispatch(tc):
        assert tc.id == "tc-seq"
        assert tc.args == {"path": "/tmp"}
        return {"status": "ok", "echo": dict(tc.args)}

    executor = make_executor(
        dispatch_fn=dispatch,
        known_tools={"read"},
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
    )

    results, intercepted, _ = executor.execute([
        ToolCall(name="read", args={"path": "/tmp"}, id="tc-seq"),
    ])

    assert not intercepted
    assert results[0]["result"]["echo"] == {"path": "/tmp"}
    assert _trace_events(logs, "tc-seq") == [
        "tool_call_received",
        "tool_call_normalized",
        "tool_call_approved",
        "tool_call",
        "tool_call_dispatch_start",
        "tool_call_dispatch_done",
        "tool_result",
        "tool_result_durable_log_visible",
        "tool_result_model_visible",
    ]
    received = logs[_log_index(logs, "tool_call_received", trace_id="tc-seq")][1]
    normalized = logs[_log_index(logs, "tool_call_normalized", trace_id="tc-seq")][1]
    model_visible = logs[_log_index(logs, "tool_result_model_visible", trace_id="tc-seq")][1]
    assert received["raw_arg_keys"] == ["path"]
    assert received["raw_arg_count"] == 1
    assert "tool_args" not in received
    assert normalized["tool_args"] == {"path": "/tmp"}
    assert normalized["removed_args"] == []
    assert model_visible["spilled"] is False


def test_api_call_id_is_stamped_on_tool_events():
    logs = []
    executor = make_executor(
        known_tools={"read"},
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
    )

    executor.execute(
        [ToolCall(name="read", args={"path": "/tmp"}, id="tc-api")],
        api_call_id="api_test123",
    )

    for event_type, fields in logs:
        assert fields.get("api_call_id") == "api_test123", event_type

    # The temporary execution context must not leak to later calls.
    logs.clear()
    executor.execute([ToolCall(name="read", args={"path": "/tmp"}, id="tc-next")])
    assert all("api_call_id" not in fields for _, fields in logs)


def test_lifecycle_trace_events_for_parallel_success_preserve_result_order():
    logs = []

    def dispatch(tc):
        if tc.name == "a":
            time.sleep(0.03)
        return {"status": "ok", "tool": tc.name}

    executor = make_executor(
        dispatch_fn=dispatch,
        parallel_safe={"a", "b"},
        known_tools={"a", "b"},
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
    )

    results, intercepted, _ = executor.execute([
        ToolCall(name="a", args={}, id="trace-a"),
        ToolCall(name="b", args={}, id="trace-b"),
    ])

    assert not intercepted
    assert [result["result"]["tool"] for result in results] == ["a", "b"]

    for trace_id in ("trace-a", "trace-b"):
        events = _trace_events(logs, trace_id)
        for required in (
            "tool_call_received",
            "tool_call_normalized",
            "tool_call_approved",
            "tool_call",
            "tool_call_dispatch_start",
            "tool_call_dispatch_done",
            "tool_result",
            "tool_result_durable_log_visible",
            "tool_result_model_visible",
        ):
            assert required in events
        assert _log_index(logs, "tool_call_received", trace_id=trace_id) < _log_index(
            logs, "tool_call_normalized", trace_id=trace_id
        )
        assert _log_index(logs, "tool_call_approved", trace_id=trace_id) < _log_index(
            logs, "tool_call_dispatch_start", trace_id=trace_id
        )
        assert _log_index(logs, "tool_call_dispatch_start", trace_id=trace_id) < _log_index(
            logs, "tool_call_dispatch_done", trace_id=trace_id
        )
        assert _log_index(logs, "tool_call_dispatch_done", trace_id=trace_id) < _log_index(
            logs, "tool_result", trace_id=trace_id
        )
        assert _log_index(logs, "tool_result_durable_log_visible", trace_id=trace_id) < _log_index(
            logs, "tool_result_model_visible", trace_id=trace_id
        )


def test_lifecycle_trace_events_for_validation_failure():
    logs = []
    executor = make_executor(
        known_tools={"read"},
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
    )
    errors = []

    results, intercepted, _ = executor.execute(
        [ToolCall(name="bogus", args={}, id="bad-trace")],
        collected_errors=errors,
    )

    assert not intercepted
    assert len(results) == 1
    error_result = results[0]["result"]
    assert error_result["status"] == "error"
    assert error_result["error_type"] == "UnknownToolError"
    assert error_result["error_phase"] == "validation"
    assert error_result["tool_name"] == "bogus"
    assert error_result["tool_call_id"] == "bad-trace"
    assert error_result["tool_trace_id"] == "bad-trace"
    assert error_result["tool_args"] == {}
    assert error_result["arg_keys"] == []
    assert error_result["validation_reason"] == "unknown_tool"
    assert error_result["available_tools"] == ["read"]
    assert error_result["_tool_error_payload_version"] == 1
    assert any("bogus" in error for error in errors)
    events = _trace_events(logs, "bad-trace")
    assert "tool_call_approved" not in events
    assert "tool_call_dispatch_start" not in events
    assert "tool_call_dispatch_failed" not in events
    validation_event = logs[
        _log_index(logs, "tool_call_validation_failed", trace_id="bad-trace")
    ][1]
    assert validation_event["reason"] == "unknown_tool"
    assert _log_index(logs, "tool_call_validation_failed", trace_id="bad-trace") < _log_index(
        logs, "tool_result_durable_log_visible", trace_id="bad-trace"
    )
    assert _log_index(logs, "tool_result_durable_log_visible", trace_id="bad-trace") < _log_index(
        logs, "tool_result_model_visible", trace_id="bad-trace"
    )


def test_lifecycle_trace_events_for_duplicate_advisory_only():
    logs = []
    dispatch_calls = []

    def dispatch(tc):
        dispatch_calls.append(tc.id)
        return {"status": "ok"}

    executor = make_executor(
        dispatch_fn=dispatch,
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
        guard=LoopGuard(max_total_calls=50, dup_free_passes=2, dup_hard_block=3),
    )

    first, intercepted, _ = executor.execute([ToolCall(name="poll", args={}, id="dup-1")])
    second, intercepted, _ = executor.execute([ToolCall(name="poll", args={}, id="dup-2")])
    advised, intercepted, _ = executor.execute([ToolCall(name="poll", args={}, id="dup-3")])

    assert not intercepted
    assert len(first) == len(second) == len(advised) == 1
    assert dispatch_calls == ["dup-1", "dup-2", "dup-3"]
    events = _trace_events(logs, "dup-3")
    assert "tool_call_validation_failed" not in events
    assert "tool_call_approved" in events
    assert "tool_call_dispatch_start" in events
    assert "tool_call_dispatch_done" in events
    payload = advised[0]["result"]
    assert payload["status"] == "ok"
    advisory = payload["_advisory"]
    assert advisory["type"] == "duplicate_tool_call"
    assert advisory["repeat_count"] == 3
    assert advisory["allowed"] is True
    assert advisory["blocked"] is False
    assert advisory["advisory_only"] is True
    assert "NOT blocked" in advisory["message"]
    assert _log_index(logs, "tool_call_dispatch_done", trace_id="dup-3") < _log_index(
        logs, "tool_result_durable_log_visible", trace_id="dup-3"
    )
    assert _log_index(logs, "tool_result_durable_log_visible", trace_id="dup-3") < _log_index(
        logs, "tool_result_model_visible", trace_id="dup-3"
    )


def test_lifecycle_trace_events_cover_spilled_result(tmp_path):
    logs = []

    def dispatch(tc):
        return {"status": "ok", "payload": "x" * 200}

    executor = make_executor(
        dispatch_fn=dispatch,
        working_dir=tmp_path,
        max_result_chars=80,
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
    )

    results, intercepted, _ = executor.execute([
        ToolCall(name="big", args={}, id="spill-trace"),
    ])

    assert not intercepted
    manifest = results[0]["result"]
    assert manifest["artifact"] == "lingtai_tool_result_spill"
    tool_block = manifest["_meta"]["tool_meta"]
    assert "spilled" not in tool_block
    assert isinstance(tool_block["char_count"], int) and tool_block["char_count"] > 0
    assert tool_block["spilled_char_count"] == manifest["original_char_count"]
    spill_event = logs[_log_index(logs, "tool_result_spilled", trace_id="spill-trace")][1]
    assert spill_event["tool_call_id"] == "spill-trace"
    model_event = logs[_log_index(logs, "tool_result_model_visible", trace_id="spill-trace")][1]
    assert model_event["spilled"] is True
    assert _log_index(logs, "tool_result_durable_log_visible", trace_id="spill-trace") < _log_index(
        logs, "tool_result_spilled", trace_id="spill-trace"
    )
    assert _log_index(logs, "tool_result_spilled", trace_id="spill-trace") < _log_index(
        logs, "tool_result_model_visible", trace_id="spill-trace"
    )


def test_lifecycle_trace_id_fallback_when_provider_id_missing():
    logs = []
    executor = make_executor(
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
    )

    results, intercepted, _ = executor.execute([ToolCall(name="manual", args={})])

    assert not intercepted
    assert len(results) == 1
    trace_ids = {
        fields.get("tool_trace_id")
        for _event, fields in logs
        if fields.get("tool_trace_id")
    }
    assert len(trace_ids) == 1
    trace_id = next(iter(trace_ids))
    assert trace_id.startswith("tool-")
    for _event, fields in logs:
        if fields.get("tool_trace_id") == trace_id:
            assert fields.get("tool_call_id") is None


def test_lifecycle_trace_events_for_dispatch_exception():
    logs = []

    def dispatch(tc):
        raise RuntimeError("boom")

    executor = make_executor(
        dispatch_fn=dispatch,
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
    )
    errors = []

    results, intercepted, _ = executor.execute(
        [ToolCall(name="explode", args={}, id="err-trace")],
        collected_errors=errors,
    )

    assert not intercepted
    error_result = results[0]["result"]
    assert error_result["status"] == "error"
    assert error_result["message"] == "boom"
    assert error_result["error_type"] == "RuntimeError"
    assert error_result["exception_type"] == "RuntimeError"
    assert error_result["error_phase"] == "dispatch"
    assert error_result["tool_name"] == "explode"
    assert error_result["tool_call_id"] == "err-trace"
    assert error_result["tool_trace_id"] == "err-trace"
    assert error_result["tool_args"] == {}
    assert "RuntimeError: boom" in error_result["traceback_tail"]
    assert error_result["_tool_error_payload_version"] == 1
    meta = error_result["tool_error"]
    assert meta["summary"] == "boom"
    assert meta["reason"] == "explode failed during dispatch: boom"
    assert meta["error_type"] == "RuntimeError"
    assert meta["error_phase"] == "dispatch"
    assert meta["tool_name"] == "explode"
    assert meta["tool_call_id"] == "err-trace"
    assert meta["retryable"] == "unknown"
    assert any("Do not blindly retry" in item for item in meta["guidance"])
    assert any("current state" in item for item in meta["guidance"])
    assert any("boom" in error for error in errors)
    events = _trace_events(logs, "err-trace")
    assert "tool_call_dispatch_failed" in events
    assert "tool_call_dispatch_done" not in events
    assert _log_index(logs, "tool_call_dispatch_start", trace_id="err-trace") < _log_index(
        logs, "tool_call_dispatch_failed", trace_id="err-trace"
    )
    assert _log_index(logs, "tool_call_dispatch_failed", trace_id="err-trace") < _log_index(
        logs, "tool_result", trace_id="err-trace"
    )
    assert _log_index(logs, "tool_result_durable_log_visible", trace_id="err-trace") < _log_index(
        logs, "tool_result_model_visible", trace_id="err-trace"
    )


def test_execute_sequential_multiple():
    order = []
    def dispatch(tc):
        order.append(tc.name)
        return {"status": "ok"}
    executor = make_executor(dispatch_fn=dispatch)
    calls = [
        ToolCall(name="a", args={}, id="1"),
        ToolCall(name="b", args={}, id="2"),
    ]
    results, intercepted, text = executor.execute(calls)
    assert len(results) == 2
    assert order == ["a", "b"]


def test_execute_parallel():
    def dispatch(tc):
        time.sleep(0.05)
        return {"status": "ok", "tool": tc.name}
    executor = make_executor(
        dispatch_fn=dispatch,
        parallel_safe={"a", "b"},
    )
    calls = [
        ToolCall(name="a", args={}, id="1"),
        ToolCall(name="b", args={}, id="2"),
    ]
    t0 = time.monotonic()
    results, intercepted, text = executor.execute(calls)
    elapsed = time.monotonic() - t0
    assert len(results) == 2
    assert elapsed < 0.15
    for result in results:
        payload = result["result"]
        assert payload["_meta"]["tool_meta"]["elapsed_ms"] > 0
        assert "_runtime_pending" not in payload
        assert "elapsed_ms" not in payload


def test_parallel_future_exception_stays_enriched_for_model(monkeypatch):
    class FakeFuture:
        def result(self):
            raise RuntimeError("worker wrapper failed")

    class FakePool:
        def __init__(self, max_workers):
            self.max_workers = max_workers
            self.futures = []

        def submit(self, *_args, **_kwargs):
            future = FakeFuture()
            self.futures.append(future)
            return future

        def shutdown(self, **_kwargs):
            pass

    monkeypatch.setattr(tool_executor_module, "ThreadPoolExecutor", FakePool)
    monkeypatch.setattr(tool_executor_module, "as_completed", lambda futures, timeout: list(futures))

    logs = []
    executor = make_executor(
        dispatch_fn=lambda tc: {"status": "ok"},
        parallel_safe={"a", "b"},
        known_tools={"a", "b"},
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
    )
    errors = []

    results, intercepted, text = executor.execute(
        [
            ToolCall(name="a", args={"x": 1}, id="trace-a"),
            ToolCall(name="b", args={"y": 2}, id="trace-b"),
        ],
        collected_errors=errors,
    )

    assert not intercepted
    assert text == ""
    assert len(results) == 2
    payload = results[0]["result"]
    assert payload["status"] == "error"
    assert payload["message"] == "worker wrapper failed"
    assert payload["error_type"] == "RuntimeError"
    assert payload["exception_type"] == "RuntimeError"
    assert payload["error_phase"] == "parallel_future"
    assert payload["tool_name"] == "a"
    assert payload["tool_call_id"] == "trace-a"
    assert payload["tool_trace_id"] == "trace-a"
    assert payload["tool_args"] == {"x": 1}
    assert payload["arg_keys"] == ["x"]
    assert "RuntimeError: worker wrapper failed" in payload["traceback_tail"]
    assert payload["_tool_error_payload_version"] == 1
    assert any("worker wrapper failed" in error for error in errors)
    log_payload = logs[_log_index(logs, "tool_result", trace_id="trace-a")][1]["result"]
    assert log_payload == payload


def test_intercept_hook():
    executor = make_executor()
    hook = MagicMock(return_value="intercepted!")
    calls = [ToolCall(name="read", args={}, id="1")]
    results, intercepted, text = executor.execute(calls, on_result_hook=hook)
    assert intercepted
    assert text == "intercepted!"


def test_on_result_hook_receives_model_visible_spill_manifest(tmp_path):
    seen = []

    def dispatch(tc):
        return {"status": "ok", "payload": "X" * 500}

    def hook(name, args, result, *, tool_call_id=None):
        seen.append((name, tool_call_id, result))
        return None

    executor = make_executor(
        dispatch_fn=dispatch,
        working_dir=tmp_path,
        max_result_chars=120,
    )

    results, intercepted, text = executor.execute(
        [ToolCall(name="read", args={}, id="spill-hook")],
        on_result_hook=hook,
    )

    assert not intercepted
    assert text == ""
    assert len(results) == 1
    assert seen[0][0] == "read"
    assert seen[0][1] == "spill-hook"
    assert seen[0][2]["status"] == "spilled"
    assert seen[0][2]["artifact"] == "lingtai_tool_result_spill"
    assert seen[0][2]["original_char_count"] > 120


def test_error_collected():
    def dispatch(tc):
        raise ValueError("something broke")
    executor = make_executor(dispatch_fn=dispatch)
    calls = [ToolCall(name="bad", args={}, id="1")]
    errors = []
    results, intercepted, text = executor.execute(calls, collected_errors=errors)
    assert len(results) == 1
    assert "bad" in errors[0]
    assert "something broke" in errors[0]


def test_tool_returned_error_is_enriched_for_agent_repair():
    def dispatch(tc):
        return {"status": "error", "message": "chat_id must be integer", "tool": "telegram"}

    executor = make_executor(dispatch_fn=dispatch)
    calls = [ToolCall(name="telegram", args={"action": "send", "chat_id": "", "text": ""}, id="tg-err")]
    errors = []

    results, intercepted, text = executor.execute(calls, collected_errors=errors)

    assert not intercepted
    assert text == ""
    payload = results[0]["result"]
    assert payload["status"] == "error"
    assert payload["message"] == "chat_id must be integer"
    assert payload["error_type"] == "telegram"
    assert payload["error_phase"] == "tool_returned_error"
    assert payload["tool_name"] == "telegram"
    assert payload["tool_call_id"] == "tg-err"
    assert payload["tool_trace_id"] == "tg-err"
    assert payload["tool_args"] == {"action": "send", "chat_id": "", "text": ""}
    assert payload["arg_keys"] == ["action", "chat_id", "text"]
    assert payload["retryable"] == "unknown"
    assert payload["_tool_error_payload_version"] == 1
    meta = payload["tool_error"]
    assert meta["summary"] == "chat_id must be integer"
    assert meta["reason"] == "telegram failed during tool_returned_error: chat_id must be integer"
    assert meta["arg_keys"] == ["action", "chat_id", "text"]
    assert meta["retryable"] == "unknown"
    assert any("Do not blindly retry" in item for item in meta["guidance"])
    assert any("correct parameters" in item for item in meta["guidance"])
    assert any("chat_id must be integer" in error for error in errors)


def test_cancel_event_stops_sequential():
    cancel = threading.Event()
    cancel.set()
    executor = make_executor()
    calls = [ToolCall(name="a", args={}, id="1")]
    results, intercepted, text = executor.execute(calls, cancel_event=cancel)
    assert results == []


def test_unknown_tool_with_known_tools():
    executor = make_executor(known_tools={"read", "write"})
    calls = [ToolCall(name="bogus", args={}, id="1")]
    errors = []
    results, intercepted, text = executor.execute(calls, collected_errors=errors)
    assert len(results) == 1
    assert any("bogus" in e for e in errors)


def test_guard_property():
    executor = make_executor()
    old_guard = executor.guard
    new_guard = LoopGuard(max_total_calls=10)
    executor.guard = new_guard
    assert executor.guard is new_guard


def test_reasoning_stripped_from_args():
    dispatched_args = []
    def dispatch(tc):
        dispatched_args.append(tc.args)
        return {"status": "ok"}
    executor = make_executor(dispatch_fn=dispatch)
    calls = [ToolCall(name="read", args={"path": "/tmp", "reasoning": "because"}, id="1")]
    executor.execute(calls)
    assert "reasoning" not in dispatched_args[0]
    assert dispatched_args[0].get("_reasoning") == "because"


def test_tool_executor_uses_meta_fn_for_stamping():
    """ToolExecutor calls meta_fn once per tool call and records the returned
    dict under result["_runtime_pending"] together with elapsed_ms.

    The real latest-only _meta.agent_meta block is promoted from _runtime_pending
    at the tool-batch boundary by meta_block.attach_active_runtime (covered in
    test_meta_block.py); ToolExecutor itself only records the pending snapshot.
    """
    meta_calls = {"n": 0}

    def meta_fn():
        meta_calls["n"] += 1
        return {"current_time": "FAKE-TS", "future_field": meta_calls["n"]}

    def dispatch(tc):
        return {"status": "ok", "echo": tc.args}

    def make_result(name, result, **kw):
        return {"name": name, "result": result, **kw}

    exe = ToolExecutor(
        dispatch_fn=dispatch,
        make_tool_result_fn=make_result,
        guard=LoopGuard(max_total_calls=10, dup_free_passes=2, dup_hard_block=8),
        known_tools={"noop"},
        parallel_safe_tools=set(),
        logger_fn=None,
        meta_fn=meta_fn,
    )
    results, intercepted, _ = exe.execute([ToolCall(id="c1", name="noop", args={})])
    assert not intercepted
    assert meta_calls["n"] == 1
    payload = results[0]["result"]
    # meta keys are recorded under _runtime_pending (not flat, not a real
    # _meta.agent_meta block — that is attached only at the turn boundary).
    pending = payload["_runtime_pending"]
    assert pending["current_time"] == "FAKE-TS"
    assert pending["future_field"] == 1
    assert "elapsed_ms" in pending
    assert "agent_meta" not in payload.get("_meta", {})
    assert "current_time" not in payload
    assert "_elapsed_ms" not in payload


def test_tool_executor_meta_fn_covers_parallel_path():
    """meta_fn is called per-tool in the parallel execution path too,
    and each result records its meta fields under _runtime_pending."""
    meta_calls = {"n": 0}

    def meta_fn():
        meta_calls["n"] += 1
        return {"current_time": "FAKE-TS"}

    def dispatch(tc):
        return {"status": "ok"}

    def make_result(name, result, **kw):
        return {"name": name, "result": result, **kw}

    exe = ToolExecutor(
        dispatch_fn=dispatch,
        make_tool_result_fn=make_result,
        guard=LoopGuard(max_total_calls=10, dup_free_passes=2, dup_hard_block=8),
        known_tools={"noop"},
        parallel_safe_tools={"noop"},  # force parallel path
        logger_fn=None,
        meta_fn=meta_fn,
    )
    results, intercepted, _ = exe.execute([
        ToolCall(id="c1", name="noop", args={}),
        ToolCall(id="c2", name="noop", args={}),
    ])
    assert not intercepted
    assert meta_calls["n"] == 2
    for r in results:
        payload = r["result"]
        # meta keys recorded under _runtime_pending
        pending = payload["_runtime_pending"]
        assert pending["current_time"] == "FAKE-TS"
        assert "elapsed_ms" in pending
        assert "agent_meta" not in payload.get("_meta", {})
        assert "current_time" not in payload
        assert "_elapsed_ms" not in payload

def test_deprecated_secondary_arg_is_ignored_not_dispatched():
    seen = []
    logs = []

    def dispatch(tc):
        seen.append((tc.name, dict(tc.args)))
        return {"status": "ok", "echo": dict(tc.args)}

    make_result = MagicMock(side_effect=lambda name, result, **kw: {"name": name, "result": result})
    executor = ToolExecutor(
        dispatch_fn=dispatch,
        make_tool_result_fn=make_result,
        guard=LoopGuard(max_total_calls=50),
        known_tools={"read", "telegram"},
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
    )

    calls = [ToolCall(
        name="read",
        args={
            "path": "/tmp",
            "secondary": {"tool": "telegram", "args": {"action": "read", "chat_id": 123}},
        },
        id="tc1",
    )]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert seen == [("read", {"path": "/tmp"})]
    payload = results[0]["result"]
    assert payload["echo"] == {"path": "/tmp"}
    assert "_secondary" not in payload
    assert any(event == "deprecated_secondary_ignored" for event, _fields in logs)


def test_deprecated_secondary_arg_is_ignored_on_parallel_path():
    seen = []
    logs = []

    def dispatch(tc):
        seen.append((tc.name, dict(tc.args)))
        return {"status": "ok", "echo": dict(tc.args)}

    make_result = MagicMock(side_effect=lambda name, result, **kw: {"name": name, "result": result})
    executor = ToolExecutor(
        dispatch_fn=dispatch,
        make_tool_result_fn=make_result,
        guard=LoopGuard(max_total_calls=50),
        known_tools={"a", "b", "telegram"},
        parallel_safe_tools={"a", "b"},
        logger_fn=lambda event_type, **fields: logs.append((event_type, fields)),
    )

    calls = [
        ToolCall(
            name="a",
            args={
                "secondary": {
                    "tool": "telegram",
                    "args": {"action": "read", "chat_id": 123},
                }
            },
            id="a1",
        ),
        ToolCall(name="b", args={"value": 2}, id="b1"),
    ]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert len(results) == 2
    assert ("a", {}) in seen
    assert ("b", {"value": 2}) in seen
    assert all("_secondary" not in result["result"] for result in results)
    assert any(event == "deprecated_secondary_ignored" for event, _fields in logs)


def test_tool_executor_attaches_batch_progress_notice_only():
    """ToolExecutor attaches the batch-scoped progress *notice* to results, but
    no longer repeats the running counter top-level.

    The counter (active_turn_tool_calls) is latest-only and lives under
    _meta.agent_meta, stamped by attach_active_runtime at the turn boundary from
    the guard's total_calls — see test_meta_block.py. The transient notice is
    still surfaced on the result that triggered it.
    """
    guard = LoopGuard(max_total_calls=10_000, notice_interval=500)
    notice = guard.record_calls(500)
    assert notice is not None

    executor = make_executor(guard=guard)
    results, intercepted, _ = executor.execute([
        ToolCall(name="read", args={"path": "/tmp"}, id="tc-progress"),
    ])

    assert not intercepted
    payload = results[0]["result"]
    # The running counter is NOT repeated top-level anymore.
    assert "active_turn_tool_calls" not in payload
    # The batch-scoped soft self-check notice is still surfaced.
    assert payload["active_turn_tool_call_notice"] == notice
    assert "active_turn_tool_call_limit" not in payload
    assert "active_turn_tool_call_notice_interval" not in payload


def test_tool_executor_runtime_counter_stamped_at_boundary():
    """The ACTIVE-turn counter reaches the model via _meta.agent_meta (latest-only),
    sourced from the guard at the tool-batch boundary by attach_active_runtime."""
    from lingtai_kernel.meta_block import attach_active_runtime
    from lingtai_kernel.llm.interface import ToolResultBlock
    from types import SimpleNamespace

    guard = LoopGuard(max_total_calls=10_000, notice_interval=500)
    guard.record_calls(500)
    executor = make_executor(guard=guard)
    agent = SimpleNamespace(_executor=executor)

    # A stamped result content (carries _runtime_pending via meta_fn-less path:
    # stamp it explicitly to mimic a time-aware agent's result).
    from lingtai_kernel.meta_block import stamp_meta
    content = {"status": "ok"}
    stamp_meta(content, {"current_time": "T"}, 7)
    block = ToolResultBlock(id="tc1", name="read", content=content)

    holder = attach_active_runtime(agent, [block], prior_holder=None)
    assert holder is content
    assert content["_meta"]["agent_meta"]["active_turn_tool_calls"] == 500
