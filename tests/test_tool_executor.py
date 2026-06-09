"""Tests for ToolExecutor — sequential and parallel tool execution."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from lingtai_kernel.llm.base import ToolCall
from lingtai_kernel.llm.interface import ToolResultBlock
from lingtai_kernel.loop_guard import LoopGuard
from lingtai_kernel.tool_executor import ToolExecutor
from lingtai_kernel.types import UnknownToolError


def make_executor(dispatch_fn=None, parallel_safe=None, known_tools=None):
    if dispatch_fn is None:
        dispatch_fn = lambda tc: {"status": "ok", "result": f"ran {tc.name}"}
    make_result = MagicMock(side_effect=lambda name, result, **kw: {"name": name, "result": result})
    guard = LoopGuard(max_total_calls=50)
    return ToolExecutor(
        dispatch_fn=dispatch_fn,
        make_tool_result_fn=make_result,
        guard=guard,
        known_tools=known_tools,
        parallel_safe_tools=parallel_safe or set(),
    )


def test_execute_single_tool():
    executor = make_executor()
    calls = [ToolCall(name="read", args={"path": "/tmp"}, id="tc1")]
    results, intercepted, text = executor.execute(calls)
    assert len(results) == 1
    assert not intercepted


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


def test_intercept_hook():
    executor = make_executor()
    hook = MagicMock(return_value="intercepted!")
    calls = [ToolCall(name="read", args={}, id="1")]
    results, intercepted, text = executor.execute(calls, on_result_hook=hook)
    assert intercepted
    assert text == "intercepted!"


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
    """ToolExecutor calls meta_fn once per tool call and merges the returned
    dict onto the result alongside _elapsed_ms."""
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
    assert payload["current_time"] == "FAKE-TS"
    assert payload["future_field"] == 1
    assert "_elapsed_ms" in payload


def test_tool_executor_meta_fn_covers_parallel_path():
    """meta_fn is called per-tool in the parallel execution path too,
    and each stamped result carries its meta fields and _elapsed_ms."""
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
        assert payload["current_time"] == "FAKE-TS"
        assert "_elapsed_ms" in payload

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
        ToolCall(name="a", args={"secondary": {"tool": "telegram", "args": {"action": "read", "chat_id": 123}}}, id="a1"),
        ToolCall(name="b", args={"value": 2}, id="b1"),
    ]

    results, intercepted, _ = executor.execute(calls)

    assert not intercepted
    assert len(results) == 2
    assert ("a", {}) in seen
    assert ("b", {"value": 2}) in seen
    assert all("_secondary" not in result["result"] for result in results)
    assert any(event == "deprecated_secondary_ignored" for event, _fields in logs)
