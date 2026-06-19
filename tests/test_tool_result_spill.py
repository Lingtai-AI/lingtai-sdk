"""Tests for the 10K-char tool-result cap and spill-to-artifact behavior.

Covers the unified hard cap applied by ``ToolExecutor`` immediately before a
result reaches the LLM wire: small results pass through unchanged; oversized
results spill to ``<workdir>/tmp/tool-results/<name>`` and the wire sees a
compact manifest pointing at the artifact.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai_kernel.llm.base import ToolCall
from lingtai_kernel.loop_guard import LoopGuard
from lingtai_kernel.tool_executor import (
    _DEFAULT_MAX_RESULT_CHARS,
    ToolExecutor,
    _spill_oversized_result,
)


CAP = _DEFAULT_MAX_RESULT_CHARS


def _serialized_len(value):
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _make_executor(*, dispatch_fn, working_dir, parallel_safe=None, known_tools=None):
    """Build a ToolExecutor wired to a tmp workdir.

    The make_tool_result_fn just echoes back what the executor passes in —
    so test assertions can look at the post-cap dict the wire would see.
    """
    captured = MagicMock(
        side_effect=lambda name, result, **kw: {
            "name": name, "result": result, "kwargs": kw,
        }
    )
    guard = LoopGuard(max_total_calls=50)
    executor = ToolExecutor(
        dispatch_fn=dispatch_fn,
        make_tool_result_fn=captured,
        guard=guard,
        known_tools=known_tools,
        parallel_safe_tools=parallel_safe or set(),
        working_dir=working_dir,
    )
    return executor, captured


# -- Pure function tests ----------------------------------------------------

def test_spill_passthrough_for_small_string():
    """Small string results survive unchanged."""
    out = _spill_oversized_result(
        "hello",
        max_chars=CAP,
        tool_name="read",
        tool_call_id="tc1",
        working_dir=None,
    )
    assert out == "hello"


def test_spill_passthrough_for_small_dict():
    """Small dict results survive unchanged (same object identity)."""
    payload = {"status": "ok", "items": [1, 2, 3]}
    out = _spill_oversized_result(
        payload,
        max_chars=CAP,
        tool_name="read",
        tool_call_id="tc1",
        working_dir=None,
    )
    assert out is payload


def test_spill_oversized_string_writes_artifact(tmp_path):
    big = "X" * (CAP * 3)
    out = _spill_oversized_result(
        big,
        max_chars=CAP,
        tool_name="read",
        tool_call_id="tc-abc",
        working_dir=tmp_path,
    )
    # Wire-bound replacement
    assert isinstance(out, dict)
    assert out["status"] == "spilled"
    assert out["tool_name"] == "read"
    assert out["tool_call_id"] == "tc-abc"
    assert out["cap_chars"] == CAP
    assert out["original_char_count"] == len(big)
    assert out["original_byte_count"] == len(big.encode("utf-8"))
    assert "warning" in out and "too large" in out["warning"]
    assert "timestamp" in out

    # Compact manifest must respect the cap
    assert _serialized_len(out) <= CAP

    # Artifact path is workdir-relative and exists
    assert out["spill_path"], "spill_path must be set when working_dir is writable"
    artifact = tmp_path / out["spill_path"]
    assert artifact.is_file()
    assert artifact.read_text(encoding="utf-8") == big


def test_spill_oversized_dict_writes_artifact(tmp_path):
    payload = {"status": "ok", "blob": "Y" * (CAP * 2), "n": 1}
    serialized = json.dumps(payload, ensure_ascii=False)
    assert len(serialized) > CAP

    out = _spill_oversized_result(
        payload,
        max_chars=CAP,
        tool_name="bash",
        tool_call_id="tc-bash",
        working_dir=tmp_path,
    )

    assert isinstance(out, dict)
    assert out["status"] == "spilled"
    assert out["tool_name"] == "bash"
    assert _serialized_len(out) <= CAP

    artifact = tmp_path / out["spill_path"]
    assert artifact.is_file()
    # The artifact stores the canonical JSON serialization of the dict
    on_disk = json.loads(artifact.read_text(encoding="utf-8"))
    assert on_disk == payload


def test_spill_handles_missing_working_dir():
    """When working_dir is None the manifest still returns, with spill_error."""
    big = "Z" * (CAP * 2)
    out = _spill_oversized_result(
        big,
        max_chars=CAP,
        tool_name="grep",
        tool_call_id=None,
        working_dir=None,
    )
    assert out["status"] == "spilled"
    assert out["spill_path"] is None
    assert "spill_error" in out
    assert _serialized_len(out) <= CAP


# -- Executor-level integration tests ---------------------------------------

def test_small_result_through_executor_unchanged(tmp_path):
    """Sub-cap results round-trip through _build_result_message untouched."""
    def dispatch(tc):
        return {"status": "ok", "result": "fine"}

    executor, captured = _make_executor(dispatch_fn=dispatch, working_dir=tmp_path)
    calls = [ToolCall(name="read", args={}, id="tc-small")]
    results, _, _ = executor.execute(calls)

    # The captured make_tool_result_fn must have seen the original dict
    args, kwargs = captured.call_args
    name, payload = args
    assert name == "read"
    assert payload["status"] == "ok"
    assert payload["result"] == "fine"
    assert "spill_path" not in payload  # nothing spilled

    # No tmp/tool-results/ directory should be created when nothing spilled
    spill_dir = tmp_path / "tmp" / "tool-results"
    assert not spill_dir.exists() or not list(spill_dir.iterdir())


def test_large_string_result_through_executor_spills(tmp_path):
    """A huge string return value is replaced with the compact manifest."""
    big = "A" * (CAP * 4)

    def dispatch(tc):
        return big  # raw string, not wrapped in a dict

    executor, captured = _make_executor(dispatch_fn=dispatch, working_dir=tmp_path)
    calls = [ToolCall(name="read", args={}, id="tc-large-str")]
    executor.execute(calls)

    name, payload = captured.call_args.args
    assert name == "read"
    assert isinstance(payload, dict)
    assert payload["status"] == "spilled"
    assert payload["tool_call_id"] == "tc-large-str"
    assert payload["original_char_count"] == len(big)
    assert _serialized_len(payload) <= CAP

    artifact = tmp_path / payload["spill_path"]
    assert artifact.read_text(encoding="utf-8") == big


def test_large_dict_result_through_executor_spills(tmp_path):
    """A dict whose serialization exceeds the cap is spilled.

    The captured wire payload contains the manifest, but its top-level shape
    still includes the runtime-added meta/timing keys stamped by stamp_meta —
    that's fine: total serialized size of the wire payload must still respect
    the cap (modulo the small meta overhead).
    """
    big_chunk = "B" * (CAP * 2)
    huge_payload = {"status": "ok", "data": [big_chunk, big_chunk]}

    def dispatch(tc):
        return huge_payload

    executor, captured = _make_executor(dispatch_fn=dispatch, working_dir=tmp_path)
    calls = [ToolCall(name="bash", args={}, id="tc-large-dict")]
    executor.execute(calls)

    name, payload = captured.call_args.args
    assert name == "bash"
    assert payload["status"] == "spilled"
    assert payload["original_char_count"] >= CAP

    # Total wire payload (with stamped meta) must still respect the cap with
    # a small grace margin for the meta block stamped after capping.
    assert _serialized_len(payload) <= CAP

    artifact = tmp_path / payload["spill_path"]
    on_disk_text = artifact.read_text(encoding="utf-8")
    # The artifact must contain the full content the agent would otherwise
    # have missed.  We don't compare equal to ``huge_payload`` directly
    # because the executor may have stamped meta onto the dict before
    # spilling; we just confirm the full big_chunk text round-tripped.
    assert big_chunk in on_disk_text


def test_parallel_large_results_get_distinct_artifacts(tmp_path):
    """Two parallel large results must land in two different artifact files."""
    big_a = "A" * (CAP * 2)
    big_b = "B" * (CAP * 2)

    def dispatch(tc):
        # Sleep so the futures genuinely overlap
        time.sleep(0.05)
        return big_a if tc.name == "alpha" else big_b

    executor, captured = _make_executor(
        dispatch_fn=dispatch,
        working_dir=tmp_path,
        parallel_safe={"alpha", "beta"},
    )
    calls = [
        ToolCall(name="alpha", args={}, id="tc-p-1"),
        ToolCall(name="beta", args={}, id="tc-p-2"),
    ]
    executor.execute(calls)

    # Both make_tool_result_fn calls captured.  Find each one's payload.
    by_name = {}
    for call in captured.call_args_list:
        n, p = call.args
        by_name[n] = p

    assert set(by_name) == {"alpha", "beta"}
    assert by_name["alpha"]["status"] == "spilled"
    assert by_name["beta"]["status"] == "spilled"

    path_a = by_name["alpha"]["spill_path"]
    path_b = by_name["beta"]["spill_path"]
    assert path_a != path_b, "parallel calls must not collide on artifact filename"

    # Each artifact holds its own full payload
    assert (tmp_path / path_a).read_text(encoding="utf-8") == big_a
    assert (tmp_path / path_b).read_text(encoding="utf-8") == big_b


def test_large_error_result_is_spilled(tmp_path):
    """A massive exception message must not blow the wire."""
    big_msg = "boom-" + ("E" * (CAP * 2))

    def dispatch(tc):
        raise RuntimeError(big_msg)

    executor, captured = _make_executor(dispatch_fn=dispatch, working_dir=tmp_path)
    calls = [ToolCall(name="read", args={}, id="tc-err")]
    executor.execute(calls)

    name, payload = captured.call_args.args
    assert payload["status"] == "spilled"
    assert _serialized_len(payload) <= CAP
    assert payload["tool_call_id"] == "tc-err"

    artifact = tmp_path / payload["spill_path"]
    on_disk = artifact.read_text(encoding="utf-8")
    assert big_msg in on_disk


def test_artifact_directory_created_lazily(tmp_path):
    """The tmp/tool-results/ directory is created only on first spill."""
    spill_dir = tmp_path / "tmp" / "tool-results"
    assert not spill_dir.exists()

    big = "X" * (CAP * 2)

    def dispatch(tc):
        return big

    executor, _ = _make_executor(dispatch_fn=dispatch, working_dir=tmp_path)
    executor.execute([ToolCall(name="read", args={}, id="tc-lazy")])

    assert spill_dir.is_dir()
    files = list(spill_dir.iterdir())
    assert len(files) == 1


def test_spill_preserves_unicode(tmp_path):
    """Non-ASCII content must survive the round trip to disk."""
    big = ("器灵 — 灵台方寸山 — " + "字" * 50) * 200  # well over CAP
    assert _serialized_len(big) > CAP

    out = _spill_oversized_result(
        big,
        max_chars=CAP,
        tool_name="read",
        tool_call_id="tc-utf8",
        working_dir=tmp_path,
    )

    artifact = tmp_path / out["spill_path"]
    assert artifact.read_text(encoding="utf-8") == big


def test_spill_artifact_preserves_full_payload_beyond_legacy_50k_cap(tmp_path):
    """Regression: the legacy 50KB lossy `_truncate_result` must NOT run on
    the primary path before the spill boundary.  A payload that exceeds both
    the 10K spill cap AND the legacy 50KB byte cap must land in the sidecar
    artifact with its full content intact — no ``[truncated — N bytes total]``
    marker, no half-dict surgery, no dropped list items.

    Pre-fix bug: `_truncate_result(result, self._max_result_bytes)` ran in
    the sequential/parallel success paths before `_build_result_message`.
    For results >50KB it lossily rewrote dict string fields and replaced
    them with a half-slice + ``"[truncated — N bytes total]"`` marker.  The
    spill then dutifully wrote that *already-truncated* dict to disk,
    losing the second half of the user's payload forever.
    """
    # Use a length above the legacy 50K byte cap so the bug would trigger
    # the destructive path if `_truncate_result` were still in the way.
    LEGACY_LEGACY_BYTE_CAP = 50_000
    payload_chunk = "Z" * (LEGACY_LEGACY_BYTE_CAP * 2)  # 100K of Zs
    assert len(payload_chunk) > LEGACY_LEGACY_BYTE_CAP

    huge_payload = {"status": "ok", "blob": payload_chunk, "marker": "TAIL"}

    def dispatch(tc):
        return huge_payload

    executor, captured = _make_executor(dispatch_fn=dispatch, working_dir=tmp_path)
    calls = [ToolCall(name="bash", args={}, id="tc-regress-50k")]
    executor.execute(calls)

    name, wire_payload = captured.call_args.args
    assert wire_payload["status"] == "spilled"
    assert _serialized_len(wire_payload) <= CAP

    # The artifact must contain the FULL blob, not a lossy half.
    artifact = tmp_path / wire_payload["spill_path"]
    on_disk_text = artifact.read_text(encoding="utf-8")
    assert payload_chunk in on_disk_text, (
        "Artifact lost the full payload — the legacy 50KB lossy truncator "
        "ran before the spill boundary and ate the second half."
    )
    # The "[truncated — N bytes total]" marker from _truncate_result MUST
    # NOT appear in the artifact.  Its presence would mean the lossy
    # rewrite happened and we're spilling the corrupted version.
    assert "[truncated" not in on_disk_text, (
        "Artifact contains a lossy-truncation marker — the legacy 50KB "
        "truncator ran on the primary path before the spill boundary."
    )
    # The original "TAIL" marker must round-trip too — it would have
    # survived since it's a small field, but its presence on disk is the
    # cleanest possible confirmation that we wrote the full dict.
    on_disk_dict = json.loads(on_disk_text)
    assert on_disk_dict["marker"] == "TAIL"
    assert on_disk_dict["blob"] == payload_chunk


def test_spill_manifest_exposes_both_relative_and_absolute_paths(tmp_path):
    """Manifest must include both workdir-relative and absolute paths so
    callers can resolve the artifact without knowing the workdir."""
    big = "A" * (CAP * 3)
    out = _spill_oversized_result(
        big,
        max_chars=CAP,
        tool_name="read",
        tool_call_id="tc-paths",
        working_dir=tmp_path,
    )
    assert out["spill_path"], "relative path required"
    assert out["spill_path_abs"], "absolute path required"

    # Relative path resolves to the same file as the absolute path.
    rel = tmp_path / out["spill_path"]
    abs_path = Path(out["spill_path_abs"])
    assert rel.is_file()
    assert abs_path.is_file()
    assert rel.resolve() == abs_path.resolve()

    # The relative path does not start with a leading slash; the absolute
    # path does (or is otherwise absolute on this platform).
    assert not out["spill_path"].startswith("/")
    assert Path(out["spill_path_abs"]).is_absolute()


def test_executor_logs_spill_event(tmp_path):
    """When a spill happens, the executor's logger_fn receives a spill event."""
    big = "Q" * (CAP * 2)
    events = []

    def dispatch(tc):
        return big

    def logger(event, **fields):
        events.append((event, fields))

    captured = MagicMock(side_effect=lambda name, result, **kw: result)
    executor = ToolExecutor(
        dispatch_fn=dispatch,
        make_tool_result_fn=captured,
        guard=LoopGuard(max_total_calls=50),
        logger_fn=logger,
        working_dir=tmp_path,
    )
    executor.execute([ToolCall(name="read", args={}, id="tc-log")])

    spill_events = [e for e in events if e[0] == "tool_result_spilled"]
    assert len(spill_events) == 1
    assert spill_events[0][1]["tool_name"] == "read"
    assert spill_events[0][1]["tool_call_id"] == "tc-log"
    assert spill_events[0][1]["original_char_count"] == len(big)

# -- Reserved-field hoisting onto spill manifest ---------------------------


def test_spill_manifest_hoists_advisory_from_oversized_dict(tmp_path):
    """``_advisory`` from LoopGuard is provider-visible and short.
    Hoist it onto the manifest so the agent still sees the loop-guard
    nudge even when the primary result spilled."""
    result = {
        "status": "ok",
        "blob": "Y" * (CAP * 3),
        "_advisory": {"type": "duplicate_tool_call", "message": "warning"},
    }
    out = _spill_oversized_result(
        result,
        max_chars=CAP,
        tool_name="read",
        tool_call_id="tc-dup",
        working_dir=tmp_path,
    )
    assert out["status"] == "spilled"
    assert out.get("_advisory") == result["_advisory"]
    assert _serialized_len(out) <= CAP


def test_spill_manifest_does_not_hoist_arbitrary_business_or_removed_secondary_keys(tmp_path):
    """Allowlist is tight: only current reserved fields hoist.

    ``_secondary`` used to be a reserved same-turn side-channel summary. The
    side channel has been removed, so a tool result that happens to contain an
    ``_secondary`` business key should live only in the artifact, not on the
    wire manifest.
    """
    result = {
        "status": "ok",
        "blob": "B" * (CAP * 3),
        "rows_processed": 12345,
        "warnings": ["row 17 skipped"],
        "_secondary": {"status": "legacy", "tool": "email", "action": "read"},
        "_advisory": {"type": "duplicate_tool_call", "message": "warn"},
    }
    out = _spill_oversized_result(
        result, max_chars=CAP, tool_name="bash", tool_call_id="tc-tight",
        working_dir=tmp_path,
    )
    assert "rows_processed" not in out
    assert "warnings" not in out
    assert "_secondary" not in out
    assert out["_advisory"] == result["_advisory"]

    artifact = tmp_path / out["spill_path"]
    on_disk = json.loads(artifact.read_text(encoding="utf-8"))
    assert on_disk["_secondary"] == result["_secondary"]


def test_spill_manifest_no_hoist_when_original_is_non_dict(tmp_path):
    """If the original is a raw string, there are no top-level reserved
    fields to hoist — the manifest is the usual shape with no extras."""
    big = "S" * (CAP * 2)
    out = _spill_oversized_result(
        big, max_chars=CAP, tool_name="read", tool_call_id="tc-str",
        working_dir=tmp_path,
    )
    assert out["status"] == "spilled"
    assert "_secondary" not in out
    assert "_advisory" not in out


def test_spill_manifest_advisory_survives_through_executor_call_site(tmp_path):
    """End-to-end: current reserved fields survive executor spill."""
    advisory = {"type": "duplicate_tool_call", "message": "warning"}

    def dispatch(tc):
        return {
            "status": "ok",
            "blob": "X" * (CAP * 3),
            "_advisory": advisory,
        }

    captured = MagicMock(side_effect=lambda name, result, **kw: result)
    executor = ToolExecutor(
        dispatch_fn=dispatch,
        make_tool_result_fn=captured,
        guard=LoopGuard(max_total_calls=50),
        working_dir=tmp_path,
    )
    executor.execute([ToolCall(name="bash", args={}, id="tc-e2e")])

    name, wire_payload = captured.call_args.args
    assert name == "bash"
    assert wire_payload["status"] == "spilled"
    assert wire_payload["_advisory"] == advisory
    assert "_secondary" not in wire_payload
    assert _serialized_len(wire_payload) <= CAP
