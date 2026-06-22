"""Tests for the ``_meta.tool_meta.comment.overflow`` per-result hint.

Jason's requested shape: capped/large model-visible tool results carry a single,
machine-generated guidance hint under ``_meta.tool_meta.comment.overflow`` that

  * notes the visible payload is capped/large,
  * points at the full original preserved in ``logs/events.jsonl`` by
    ``tool_call_id`` (NOT an external ``saved_path``/sidecar file),
  * explains retrieval (grep / ``lingtai-agent log query`` / delegate to a
    daemon/subagent), and
  * recommends ``system(action="summarize")`` after consuming the result.

It must be exactly one comment topic (``overflow``) — never split into parallel
``comment.retrieval`` / ``comment.summarize`` headings — and must not disturb the
existing ``tool_meta`` identity fields (``id``/``char_count``/``elapsed_ms``).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from lingtai_kernel.llm.base import ToolCall
from lingtai_kernel.loop_guard import LoopGuard
from lingtai_kernel.meta_block import build_tool_meta_overflow_comment
from lingtai_kernel.tool_executor import (
    _DEFAULT_MAX_RESULT_CHARS,
    ToolExecutor,
)


def _make_executor(
    *, dispatch_fn, working_dir, max_result_chars=_DEFAULT_MAX_RESULT_CHARS,
    summarize_notification_threshold=None,
):
    captured = MagicMock(side_effect=lambda name, result, **kw: result)
    executor = ToolExecutor(
        dispatch_fn=dispatch_fn,
        make_tool_result_fn=captured,
        guard=LoopGuard(max_total_calls=50),
        working_dir=working_dir,
        max_result_chars=max_result_chars,
        summarize_notification_threshold=summarize_notification_threshold,
    )
    return executor, captured


def _tool_meta(wire_payload):
    assert isinstance(wire_payload, dict), wire_payload
    return wire_payload.get("_meta", {}).get("tool_meta", {})


# -- builder unit tests -----------------------------------------------------

def test_builder_single_overflow_topic_with_required_subkeys():
    comment = build_tool_meta_overflow_comment("tc-abc")
    assert set(comment.keys()) == {
        "summary",
        "full_original",
        "how_to_retrieve",
        "after_consuming",
    }
    # Single topic only: no retrieval/summarize sibling headings.
    assert "retrieval" not in comment
    assert "summarize" not in comment


def test_builder_references_events_jsonl_and_call_id_not_saved_path():
    comment = build_tool_meta_overflow_comment("tc-xyz")
    blob = json.dumps(comment)
    assert "logs/events.jsonl" in blob
    assert "tool_call_id=tc-xyz" in blob or "tc-xyz" in comment["full_original"]
    # Must NOT point at an external saved_path / sidecar tool-results file.
    assert "saved_path" not in blob
    assert "tmp/tool-results" not in blob


def test_builder_mentions_retrieval_and_summarize_within_overflow():
    comment = build_tool_meta_overflow_comment("tc-1")
    assert "grep" in comment["how_to_retrieve"]
    assert "lingtai-agent log query" in comment["how_to_retrieve"]
    assert "daemon" in comment["how_to_retrieve"] or "subagent" in comment["how_to_retrieve"]
    assert "summarize" in comment["after_consuming"]


# -- spilled (capped) result ------------------------------------------------

def test_spilled_result_carries_overflow_comment(tmp_path):
    """A result spilled over the cap gets _meta.tool_meta.comment.overflow."""
    def dispatch(tc):
        return {"data": "Z" * 1200}

    executor, captured = _make_executor(
        dispatch_fn=dispatch, working_dir=tmp_path, max_result_chars=500,
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-spill")])
    _, wire = captured.call_args.args
    assert wire["status"] == "spilled"

    tool_meta = _tool_meta(wire)
    overflow = tool_meta.get("comment", {}).get("overflow")
    assert overflow is not None, tool_meta
    # references events.jsonl + this call id, not saved_path
    blob = json.dumps(tool_meta["comment"])
    assert "logs/events.jsonl" in blob
    assert "tc-spill" in blob
    assert "saved_path" not in blob
    # No parallel comment headings.
    assert set(tool_meta["comment"].keys()) == {"overflow"}

    # Existing identity fields remain intact.
    assert tool_meta["id"] == "tc-spill"
    assert isinstance(tool_meta["char_count"], int)
    assert isinstance(tool_meta["elapsed_ms"], int)
    assert "spilled_char_count" in tool_meta


# -- large-but-inline result ------------------------------------------------

def test_large_inline_result_carries_overflow_comment(tmp_path):
    """A large (over hint threshold) but un-spilled result gets the comment."""
    def dispatch(tc):
        return {"data": "Q" * 400}

    # High spill cap so the result stays inline, low hint threshold so it counts
    # as "large".
    executor, captured = _make_executor(
        dispatch_fn=dispatch,
        working_dir=tmp_path,
        max_result_chars=_DEFAULT_MAX_RESULT_CHARS,
        summarize_notification_threshold=100,
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-large")])
    _, wire = captured.call_args.args
    assert wire.get("status") != "spilled"

    tool_meta = _tool_meta(wire)
    assert tool_meta["char_count"] > 100
    overflow = tool_meta.get("comment", {}).get("overflow")
    assert overflow is not None, tool_meta
    assert "logs/events.jsonl" in overflow["full_original"]
    assert "tc-large" in overflow["full_original"]


# -- small result: no comment -----------------------------------------------

def test_small_result_has_no_overflow_comment(tmp_path):
    """An ordinary small result does NOT carry the overflow comment."""
    def dispatch(tc):
        return {"ok": True}

    executor, captured = _make_executor(
        dispatch_fn=dispatch,
        working_dir=tmp_path,
        summarize_notification_threshold=100,
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-small")])
    _, wire = captured.call_args.args

    tool_meta = _tool_meta(wire)
    assert "comment" not in tool_meta, tool_meta
    # Identity fields still present and intact.
    assert tool_meta["id"] == "tc-small"
    assert isinstance(tool_meta["char_count"], int)
    assert isinstance(tool_meta["elapsed_ms"], int)


def test_large_result_no_comment_when_hint_disabled(tmp_path):
    """When the hint threshold is disabled (0), only spills earn the comment."""
    def dispatch(tc):
        return {"data": "Q" * 4000}

    executor, captured = _make_executor(
        dispatch_fn=dispatch,
        working_dir=tmp_path,
        max_result_chars=_DEFAULT_MAX_RESULT_CHARS,  # no spill
        summarize_notification_threshold=0,  # disabled
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-nohint")])
    _, wire = captured.call_args.args
    assert wire.get("status") != "spilled"
    tool_meta = _tool_meta(wire)
    assert "comment" not in tool_meta, tool_meta
