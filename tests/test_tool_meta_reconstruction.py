"""Tests for the one-shot reconstruction event on ``_meta.tool_meta`` (channel A).

When the runtime performs an actual delayed-summarize reconstruction, the
adapter records a pending before-context (A) event. The kernel attaches the
A->B event to the NEXT visible tool result's ``_meta.tool_meta.reconstruction``
exactly once, then clears it (one-shot). This rides on the permanent
``tool_meta`` block (per-result evidence), NOT the latest-only ``agent_meta``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from lingtai_kernel.llm.base import ToolCall
from lingtai_kernel.loop_guard import LoopGuard
from lingtai_kernel.tool_executor import (
    _DEFAULT_MAX_RESULT_CHARS,
    ToolExecutor,
)


def _make_executor(*, dispatch_fn, working_dir, reconstruction_event_fn=None):
    captured = MagicMock(side_effect=lambda name, result, **kw: result)
    executor = ToolExecutor(
        dispatch_fn=dispatch_fn,
        make_tool_result_fn=captured,
        guard=LoopGuard(max_total_calls=50),
        working_dir=working_dir,
        max_result_chars=_DEFAULT_MAX_RESULT_CHARS,
        reconstruction_event_fn=reconstruction_event_fn,
    )
    return executor, captured


def _tool_meta(wire):
    assert isinstance(wire, dict), wire
    return wire.get("_meta", {}).get("tool_meta", {})


_EVENT = {
    "type": "delayed_summarize_reconstruction",
    "trigger_threshold": 0.75,
    "recovery_target": 0.60,
    "context_window": 100000,
    "before": {"context_tokens": 85000, "usage": 0.85},
    "after": {"context_tokens": 70000, "usage": 0.70},
    "molt": {
        "level": "warning",
        "action": "summarize_reconstruction_attempted_still_above_0_6_consider_molt",
    },
}


def test_reconstruction_event_attaches_to_tool_meta(tmp_path):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return _EVENT if calls["n"] == 1 else None

    executor, captured = _make_executor(
        dispatch_fn=lambda tc: {"ok": True},
        working_dir=tmp_path,
        reconstruction_event_fn=fn,
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-1")])
    _, wire = captured.call_args.args

    tm = _tool_meta(wire)
    assert tm["id"] == "tc-1"
    # Event rides on tool_meta (permanent), not agent_meta.
    assert tm["reconstruction"]["type"] == "delayed_summarize_reconstruction"
    assert tm["reconstruction"]["before"]["usage"] == 0.85
    assert tm["reconstruction"]["after"]["usage"] == 0.70
    assert tm["reconstruction"]["molt"]["level"] == "warning"


def test_reconstruction_event_is_one_shot_across_batch(tmp_path):
    """Within a multi-result batch, only the first result carries the event;
    the one-shot source returns None thereafter."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return _EVENT if calls["n"] == 1 else None

    executor, captured = _make_executor(
        dispatch_fn=lambda tc: {"ok": True},
        working_dir=tmp_path,
        reconstruction_event_fn=fn,
    )
    executor.execute(
        [
            ToolCall(name="read", args={}, id="tc-a"),
            ToolCall(name="read", args={}, id="tc-b"),
        ]
    )
    wires = [c.args[1] for c in captured.call_args_list]
    with_event = [w for w in wires if "reconstruction" in _tool_meta(w)]
    assert len(with_event) == 1


def test_no_event_when_none_pending(tmp_path):
    executor, captured = _make_executor(
        dispatch_fn=lambda tc: {"ok": True},
        working_dir=tmp_path,
        reconstruction_event_fn=lambda: None,
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-x")])
    _, wire = captured.call_args.args
    assert "reconstruction" not in _tool_meta(wire)


def test_no_fn_configured_is_safe(tmp_path):
    executor, captured = _make_executor(
        dispatch_fn=lambda tc: {"ok": True},
        working_dir=tmp_path,
        reconstruction_event_fn=None,
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-y")])
    _, wire = captured.call_args.args
    tm = _tool_meta(wire)
    assert tm["id"] == "tc-y"
    assert "reconstruction" not in tm
