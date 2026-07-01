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
from lingtai_kernel.meta_block import (
    TOOL_META_CONTEXT_EVENT_PENDING_KEY,
    TOOL_META_CONTEXT_PENDING_KEY,
    stamp_meta,
)
from lingtai_kernel.reminders.context_pressure import (
    CURRENT_MOLT_EVENT,
    CURRENT_MOLT_TARGET_PATH,
)
from lingtai_kernel.tool_executor import (
    _DEFAULT_MAX_RESULT_CHARS,
    ToolExecutor,
)


def _make_executor(
    *,
    dispatch_fn,
    working_dir,
    reconstruction_event_fn=None,
    logger_fn=None,
    meta_fn=None,
):
    captured = MagicMock(side_effect=lambda name, result, **kw: result)
    executor = ToolExecutor(
        dispatch_fn=dispatch_fn,
        make_tool_result_fn=captured,
        guard=LoopGuard(max_total_calls=50),
        working_dir=working_dir,
        max_result_chars=_DEFAULT_MAX_RESULT_CHARS,
        reconstruction_event_fn=reconstruction_event_fn,
        logger_fn=logger_fn,
        meta_fn=meta_fn,
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
    "molt": (
        "The runtime already rebuilt the provider context after summarization, "
        "but the rebuilt context is still at 70% of the context window, at or "
        "above the 60% recovery target. If more digested tool results can be "
        "summarized, do that first; otherwise tend durable stores and molt "
        "deliberately. See psyche-manual."
    ),
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
    assert isinstance(tm["reconstruction"]["molt"], str)
    assert "runtime already rebuilt the provider context" in tm["reconstruction"]["molt"]
    assert "molt deliberately" in tm["reconstruction"]["molt"]


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


# ---------------------------------------------------------------------------
# Reconstruction reminder-emission event: logged ONLY when the reconstruction
# event actually carries the molt reminder text at tool_meta.reconstruction.molt.
# ---------------------------------------------------------------------------


def _capture_logger():
    events: list[tuple[str, dict]] = []
    return events, (lambda event_type, **fields: events.append((event_type, fields)))


def test_reconstruction_event_emits_reminder_event_when_molt_attached(tmp_path):
    events, logger = _capture_logger()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return dict(_EVENT) if calls["n"] == 1 else None

    executor, captured = _make_executor(
        dispatch_fn=lambda tc: {"ok": True},
        working_dir=tmp_path,
        reconstruction_event_fn=fn,
        logger_fn=logger,
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-1")])
    _, wire = captured.call_args.args
    # The reminder text stays at tool_meta.reconstruction.molt (NOT moved).
    assert isinstance(_tool_meta(wire)["reconstruction"]["molt"], str)

    emitted = [e for e in events if e[0] == "context_pressure_reconstruction_molt_reminder_emitted"]
    assert len(emitted) == 1
    payload = emitted[0][1]
    assert payload["target_path"] == "_meta.tool_meta.reconstruction.molt"
    assert payload["before_usage"] == 0.85
    assert payload["after_usage"] == 0.70
    assert payload["branch"] == "above_recovery"
    assert payload["trigger_threshold"] == 0.75
    assert payload["recovery_target"] == 0.60
    # Redaction-safe: no full reminder prose in the event payload.
    import json

    assert "runtime already rebuilt" not in json.dumps(payload)


def test_reconstruction_event_no_reminder_event_when_no_molt(tmp_path):
    # Event below the recovery target carries no molt -> no reminder event, but
    # the structured evidence is still attached.
    events, logger = _capture_logger()
    event_no_molt = {k: v for k, v in _EVENT.items() if k != "molt"}

    executor, captured = _make_executor(
        dispatch_fn=lambda tc: {"ok": True},
        working_dir=tmp_path,
        reconstruction_event_fn=lambda: event_no_molt,
        logger_fn=logger,
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-1")])
    _, wire = captured.call_args.args
    assert "reconstruction" in _tool_meta(wire)
    assert "molt" not in _tool_meta(wire)["reconstruction"]
    assert not any(
        e[0] == "context_pressure_reconstruction_molt_reminder_emitted" for e in events
    )


# ---------------------------------------------------------------------------
# Current sustained-pressure molt reminder: PERMANENT tool_meta.context.molt,
# with a deduped emission event.  build_meta stashes the reminder (and, on a new
# emission, the event payload) under transit keys; _attach_tool_block promotes
# the reminder into tool_meta.context and logs the event.
# ---------------------------------------------------------------------------


_CURRENT_MOLT_TEXT = "Context has stayed high across 3 consecutive fresh model calls ..."
_CURRENT_MOLT_EVENT_PAYLOAD = {
    "target_path": CURRENT_MOLT_TARGET_PATH,
    "message_hash": "abcdef012345",
    "threshold_high": 0.75,
    "recovery_target": 0.60,
    "usage": 0.90,
    "streak": 3,
    "last_round_id": 7,
    "transition_reason": "warning_active",
}


def _current_molt_meta(*, with_event: bool):
    meta = {TOOL_META_CONTEXT_PENDING_KEY: {"molt": _CURRENT_MOLT_TEXT}}
    if with_event:
        meta[TOOL_META_CONTEXT_EVENT_PENDING_KEY] = {
            "event_name": CURRENT_MOLT_EVENT,
            "payload": dict(_CURRENT_MOLT_EVENT_PAYLOAD),
        }
    return meta


def test_current_molt_promoted_to_tool_meta_context_and_emits_event(tmp_path):
    events, logger = _capture_logger()
    executor, captured = _make_executor(
        dispatch_fn=lambda tc: {"ok": True},
        working_dir=tmp_path,
        logger_fn=logger,
        meta_fn=lambda: _current_molt_meta(with_event=True),
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-1")])
    _, wire = captured.call_args.args

    # Reminder text landed on PERMANENT tool_meta.context.molt.
    tm = _tool_meta(wire)
    assert tm["context"]["molt"] == _CURRENT_MOLT_TEXT
    # ...and NOT in an agent_meta block (that stays sparse and molt-free now).
    assert "agent_meta" not in wire.get("_meta", {})

    emitted = [e for e in events if e[0] == CURRENT_MOLT_EVENT]
    assert len(emitted) == 1
    assert emitted[0][1]["target_path"] == "_meta.tool_meta.context.molt"
    assert emitted[0][1]["streak"] == 3
    assert emitted[0][1]["last_round_id"] == 7


def test_current_molt_attached_without_event_payload_does_not_emit(tmp_path):
    # When build_meta deduped the emission (no event payload in the transit dict),
    # the reminder text is still attached permanently but NO event is logged.
    events, logger = _capture_logger()
    executor, captured = _make_executor(
        dispatch_fn=lambda tc: {"ok": True},
        working_dir=tmp_path,
        logger_fn=logger,
        meta_fn=lambda: _current_molt_meta(with_event=False),
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-1")])
    _, wire = captured.call_args.args

    assert _tool_meta(wire)["context"]["molt"] == _CURRENT_MOLT_TEXT
    assert not any(e[0] == CURRENT_MOLT_EVENT for e in events)


def test_no_current_molt_means_no_context_block_and_no_event(tmp_path):
    events, logger = _capture_logger()
    executor, captured = _make_executor(
        dispatch_fn=lambda tc: {"ok": True},
        working_dir=tmp_path,
        logger_fn=logger,
        meta_fn=lambda: {},
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-1")])
    _, wire = captured.call_args.args

    assert "context" not in _tool_meta(wire)
    assert not any(e[0] == CURRENT_MOLT_EVENT for e in events)


def test_current_molt_event_deduped_across_results_in_same_round(tmp_path):
    # The reminder text is permanent (restamped on every result), but the emission
    # event is deduped by last_round_id: two results in the SAME round produce the
    # text twice but log the event once; a new round re-emits.
    events, logger = _capture_logger()
    round_state = {"rid": 7}

    def meta_fn():
        meta = _current_molt_meta(with_event=True)
        meta[TOOL_META_CONTEXT_EVENT_PENDING_KEY]["payload"]["last_round_id"] = round_state["rid"]
        return meta

    executor, captured = _make_executor(
        dispatch_fn=lambda tc: {"ok": True},
        working_dir=tmp_path,
        logger_fn=logger,
        meta_fn=meta_fn,
    )
    # Two results in the same round (rid=7).
    executor.execute(
        [ToolCall(name="read", args={}, id="tc-a"), ToolCall(name="read", args={}, id="tc-b")]
    )
    # Every result still carries the permanent reminder text.
    for call in captured.call_args_list:
        assert call.args[1]["_meta"]["tool_meta"]["context"]["molt"] == _CURRENT_MOLT_TEXT
    # ...but the event was logged only ONCE for round 7.
    assert sum(1 for e in events if e[0] == CURRENT_MOLT_EVENT) == 1

    # A new provider round re-arms the event.
    round_state["rid"] = 8
    executor.execute([ToolCall(name="read", args={}, id="tc-c")])
    assert sum(1 for e in events if e[0] == CURRENT_MOLT_EVENT) == 2
